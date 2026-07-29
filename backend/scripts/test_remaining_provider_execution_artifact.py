from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path


_TMP_HOME = tempfile.mkdtemp(prefix="remaining_provider_execution_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import provider_manifest  # noqa: E402
from execution_artifact_io import requires_execution_artifact  # noqa: E402
from provider_session_events_execution import (  # noqa: E402
    external_execution_kinds,
    strategy_for,
)


REMAINING_KINDS = frozenset({
    "amp",
    "copilot",
    "cursor",
    "kimi",
    "openai",
    "opencode",
    "pi",
    "qwen",
})


def _class_method(path: Path, class_name: str, method_name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == method_name:
                return item
    raise AssertionError(f"{path.name}:{class_name}.{method_name} is missing")


def _call_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            names.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
    return names


def test_manifest_covers_every_production_start_with_an_artifact() -> None:
    local = frozenset(
        kind
        for kind, spec in provider_manifest.SPECS.items()
        if not spec.virtual
    )
    assert provider_manifest.artifact_family_kinds() == (
        local - {"codex", "fugu"}
    )
    assert external_execution_kinds() == REMAINING_KINDS
    assert {
        kind
        for kind in local
        if requires_execution_artifact(kind)
    } == local
    assert {
        strategy_for(kind).kind
        for kind in REMAINING_KINDS
    } == REMAINING_KINDS


def test_remaining_provider_start_methods_are_thin_artifact_consumers() -> None:
    forbidden = {
        "available_models",
        "get",
        "get_fields",
        "get_context_strategy",
        "resolve_cli_binary",
        "resolve_extension_run_policy",
        "resolve_for_run",
        "runner_argv",
        "popen_runner",
    }
    for kind in REMAINING_KINDS:
        spec = provider_manifest.SPECS[kind]
        path = BACKEND / f"{spec.module}.py"
        method = _class_method(path, spec.cls, "_start_run")
        calls = _call_names(method)
        assert "start_session_events_execution" in calls, kind
        assert not (calls & forbidden), (kind, sorted(calls & forbidden))
        source = ast.get_source_segment(
            path.read_text(encoding="utf-8"),
            method,
        ) or ""
        assert "self.record" not in source, kind


def test_remaining_provider_env_uses_admitted_runtime_record() -> None:
    credential_kinds = {"amp", "openai", "qwen"}
    for kind in REMAINING_KINDS:
        spec = provider_manifest.SPECS[kind]
        path = BACKEND / f"{spec.module}.py"
        method = _class_method(path, spec.cls, "build_env")
        source = ast.get_source_segment(
            path.read_text(encoding="utf-8"),
            method,
        ) or ""
        assert "self.record" not in source, kind
        if kind in credential_kinds:
            assert "self.runtime_record()" in source, kind


def test_remaining_runners_restore_artifact_and_never_resolve_cli_late() -> None:
    shared = (
        BACKEND / "provider_session_events_runner.py"
    ).read_text(encoding="utf-8")
    assert "restore_family_runner_runtime" in shared
    assert "materialize_sdk(" in shared
    for kind in REMAINING_KINDS:
        module = provider_manifest.runner_module_for(kind)
        path = BACKEND / f"{module}.py"
        source = path.read_text(encoding="utf-8")
        assert "restore_session_events_runner" in source, kind
        assert "resolve_cli_binary(" not in source, kind


def test_every_provider_consumes_persisted_authority_before_spawn() -> None:
    provider_source = (BACKEND / "provider.py").read_text(encoding="utf-8")
    assert "attest_execution_spawn_authority(artifact)" in provider_source
    for filename in (
        "provider_agy.py",
        "provider_claude.py",
        "provider_codex.py",
        "provider_session_events.py",
    ):
        source = (BACKEND / filename).read_text(encoding="utf-8")
        assert "consume_execution_spawn_authority(" in source, filename
    codex_source = (BACKEND / "provider_codex.py").read_text(encoding="utf-8")
    assert "open_pinned_runner_launch(" in codex_source
    assert "runner_argv(" not in codex_source


def test_recovery_retries_every_artifact_family() -> None:
    source = (BACKEND / "run_recovery.py").read_text(encoding="utf-8")
    assert source.count("_provider_manifest.artifact_family_kinds()") >= 2
    assert 'original_artifact.provider_kind in {"claude", "agy"}' not in source


TESTS = (
    test_manifest_covers_every_production_start_with_an_artifact,
    test_remaining_provider_start_methods_are_thin_artifact_consumers,
    test_remaining_provider_env_uses_admitted_runtime_record,
    test_remaining_runners_restore_artifact_and_never_resolve_cli_late,
    test_every_provider_consumes_persisted_authority_before_spawn,
    test_recovery_retries_every_artifact_family,
)


if __name__ == "__main__":
    for test in TESTS:
        test()
    print("PASS remaining provider execution artifact")
