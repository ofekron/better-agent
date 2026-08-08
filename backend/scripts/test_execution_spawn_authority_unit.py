"""Hermetic unit coverage for execution_spawn_authority.

The integration owners (test_codex_execution_artifact, test_claude_execution_artifact,
test_shared_execution_authority, test_remote_admission_lifecycle) drive the real
attest path through materialized Codex/Fugu/family runtimes and leave the
dispatch/error branches at ~82%. Several of them stub past
attest_execution_spawn_authority / consume_execution_spawn_authority entirely, so
the module's own logic is not their subject. This file closes every residual
branch hermetically: the codex admit-vs-reject decision, the family dispatch,
the no-authority rejection for dict / non-dict / missing contracts, the generic
exception wrapping in the public attest wrapper, and both consume outcomes
(persisted-match admits, persisted-mismatch rejects).

The late-bound provider runtimes and the artifact loader are replaced with
in-process fakes through sys.modules; the timing context manager and the family
kind set are patched for determinism. No provider runtime, filesystem, or real
keychain is touched.
"""

from __future__ import annotations

import contextlib
import sys
import types
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

_test_home.isolate("bc-test-execution-spawn-authority-unit-")

import execution_spawn_authority as esa  # noqa: E402
from execution_template import ExecutionAuthorityError  # noqa: E402


class _FakeArtifact:
    """Exposes only the surface execution_spawn_authority reads.

    provider_contract can be seeded with an exception to exercise the wrapper's
    generic-exception arm.
    """

    def __init__(self, *, provider_kind: str, provider_contract: Any) -> None:
        self.provider_kind = provider_kind
        self._provider_contract = provider_contract

    @property
    def provider_contract(self) -> Any:
        if isinstance(self._provider_contract, BaseException):
            raise self._provider_contract
        return self._provider_contract


def _module(name: str, **attrs: Any) -> ModuleType:
    mod = ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


@pytest.fixture(autouse=True)
def _hermetic_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the timing context manager from touching any provider runtime."""

    @contextlib.contextmanager
    def _noop(_name: str):  # noqa: ANN202
        yield

    monkeypatch.setattr(esa, "timed_contract_step", _noop)


def _codex_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    attest: Any,
    contract_from_artifact: Any | None = None,
) -> dict[str, list]:
    calls: dict[str, list] = {"launch": [], "manifest": []}

    def _contract(artifact):  # noqa: ANN001, ANN202
        if contract_from_artifact is not None:
            return contract_from_artifact(artifact)
        return types.SimpleNamespace(attest=attest)

    fake = _module(
        "codex_execution_runtime",
        codex_contract_from_artifact=_contract,
        codex_runner_launch_from_artifact=lambda a: calls["launch"].append(a),
        codex_runtime_agent_manifest=lambda a: calls["manifest"].append(a),
    )
    monkeypatch.setitem(sys.modules, "codex_execution_runtime", fake)
    return calls


def _family_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kinds: frozenset[str],
) -> dict[str, list]:
    calls: dict[str, list] = {"launch": [], "capability": []}
    monkeypatch.setattr(esa, "artifact_family_kinds", lambda: kinds)
    fake = _module(
        "provider_family_execution_runtime",
        family_launch_from_artifact=lambda a: calls["launch"].append(a),
        family_capability_manifest_from_artifact=lambda a: calls["capability"].append(a),
    )
    monkeypatch.setitem(sys.modules, "provider_family_execution_runtime", fake)
    return calls


def test_attest_codex_contract_admits_and_launches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _codex_runtime(monkeypatch, attest=lambda: True)
    artifact = _FakeArtifact(provider_kind="codex", provider_contract={"type": "codex"})

    assert esa.attest_execution_spawn_authority(artifact) is None
    assert calls["launch"] == [artifact]
    assert calls["manifest"] == [artifact]


def test_attest_rejects_codex_authority_changed_before_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _codex_runtime(monkeypatch, attest=lambda: False)
    artifact = _FakeArtifact(provider_kind="fugu", provider_contract={"type": "codex"})

    with pytest.raises(
        ExecutionAuthorityError,
        match="Codex process authority changed before admission",
    ):
        esa.attest_execution_spawn_authority(artifact)


def test_attest_wraps_unexpected_runtime_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_artifact: _FakeArtifact) -> None:
        raise ValueError("runtime blew up")

    _codex_runtime(monkeypatch, attest=lambda: True, contract_from_artifact=_boom)
    artifact = _FakeArtifact(provider_kind="codex", provider_contract={"type": "codex"})

    with pytest.raises(
        ExecutionAuthorityError,
        match="execution spawn authority is invalid",
    ) as exc_info:
        esa.attest_execution_spawn_authority(artifact)

    assert isinstance(exc_info.value.__cause__, ValueError)


def test_attest_family_contract_launches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _family_runtime(monkeypatch, kinds=frozenset({"claude", "claude_ba"}))
    artifact = _FakeArtifact(provider_kind="claude", provider_contract={"type": "claude"})

    assert esa.attest_execution_spawn_authority(artifact) is None
    assert calls["launch"] == [artifact]
    assert calls["capability"] == [artifact]


def test_attest_rejects_provider_with_no_spawn_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _family_runtime(monkeypatch, kinds=frozenset({"claude"}))
    artifact = _FakeArtifact(provider_kind="claude", provider_contract={"type": "unknown"})

    with pytest.raises(
        ExecutionAuthorityError,
        match="execution provider has no spawn authority",
    ):
        esa.attest_execution_spawn_authority(artifact)


def test_attest_treats_non_dict_contract_as_no_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _family_runtime(monkeypatch, kinds=frozenset({"claude"}))
    artifact = _FakeArtifact(provider_kind="claude", provider_contract=["not", "a", "dict"])

    with pytest.raises(
        ExecutionAuthorityError,
        match="execution provider has no spawn authority",
    ):
        esa.attest_execution_spawn_authority(artifact)


def test_attest_treats_missing_contract_as_no_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _family_runtime(monkeypatch, kinds=frozenset({"claude"}))
    artifact = _FakeArtifact(provider_kind="claude", provider_contract=None)

    with pytest.raises(
        ExecutionAuthorityError,
        match="execution provider has no spawn authority",
    ):
        esa.attest_execution_spawn_authority(artifact)


def test_consume_admits_when_persisted_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = _FakeArtifact(provider_kind="claude", provider_contract={"type": "claude"})
    admitted = _family_runtime(monkeypatch, kinds=frozenset({"claude"}))
    loaded: list[tuple[Path, bool]] = []

    def _load(run_dir: Path, *, validate_input: bool):  # noqa: ANN202
        loaded.append((run_dir, validate_input))
        return artifact

    monkeypatch.setitem(
        sys.modules,
        "execution_artifact_io",
        _module("execution_artifact_io", load_execution_artifact=_load),
    )

    run_dir = Path("/nonexistent/run")
    assert esa.consume_execution_spawn_authority(artifact, run_dir) is None
    assert loaded == [(run_dir, True)]
    assert admitted["launch"] == [artifact]


def test_consume_rejects_when_persisted_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = _FakeArtifact(provider_kind="claude", provider_contract={"type": "claude"})
    other = _FakeArtifact(provider_kind="codex", provider_contract={"type": "codex"})

    monkeypatch.setitem(
        sys.modules,
        "execution_artifact_io",
        _module(
            "execution_artifact_io",
            load_execution_artifact=lambda _run_dir, *, validate_input: other,
        ),
    )

    with pytest.raises(
        ExecutionAuthorityError,
        match="persisted execution authority changed before spawn",
    ):
        esa.consume_execution_spawn_authority(artifact, Path("/nonexistent/run"))
