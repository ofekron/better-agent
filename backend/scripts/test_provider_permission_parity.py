#!/usr/bin/env python3
"""Every CLI-spawning provider resolves a permission and hands it to its runner.

Runners fail closed: they add a bypass flag only when the resolved permission
explicitly asks for one. A provider kind with no entry in `permission._AXES`
therefore resolves to `{}`, the runner adds nothing, and the CLI — which runs
headless and cannot prompt — auto-denies every tool call. The turn still
"succeeds" having done nothing, which is why this failed silently.

That is what happened to `agy` (no axes, and the provider never passed
`permission` at all) and to `fugu` (drives codex but had no axes of its own).
"""
from __future__ import annotations

import ast
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="permission_parity_test_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import permission  # noqa: E402
import provider_manifest  # noqa: E402

# Kinds with no permission surface of their own. `kimi` hardcodes `--yolo` in
# its runner, so it never consults the resolver; `copilot` exposes no
# permission flag at all. Both are recorded here so adding one is a visible
# change rather than an accident.
KINDS_WITHOUT_PERMISSION_AXES = {"kimi", "copilot"}


def _spawning_kinds() -> list[str]:
    out = []
    for kind in provider_manifest.all_kinds():
        spec = provider_manifest.spec_for(kind)
        if spec and spec.runner_module:
            out.append(kind)
    return out


def test_every_spawning_kind_has_a_permission_default():
    missing = sorted(
        kind
        for kind in _spawning_kinds()
        if kind not in KINDS_WITHOUT_PERMISSION_AXES
        and not permission.default_permission_for_kind(kind)
    )
    assert not missing, (
        f"these kinds resolve an empty permission, so their runner denies every "
        f"tool: {missing}"
    )


def test_agy_defaults_to_full_permission():
    assert permission.default_permission_for_kind("agy") == {
        "mode": "dangerously-skip-permissions"
    }


def test_fugu_shares_codex_permission():
    """Fugu is the codex binary; a separate table would be a second source of
    truth that could drift."""
    assert permission.default_permission_for_kind("fugu") == (
        permission.default_permission_for_kind("codex")
    )
    assert permission.permission_axes_for_kind("fugu") == (
        permission.permission_axes_for_kind("codex")
    )


def _passes_permission_to_runner(module_name: str) -> bool:
    """True when the module builds a runner input payload carrying a
    `permission` key. Parsed rather than imported so this stays a pure static
    check with no provider side effects."""
    source = (_BACKEND / f"{module_name}.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key in node.keys:
            if isinstance(key, ast.Constant) and key.value == "permission":
                return True
    return False


def test_every_spawning_provider_passes_permission_into_run_inputs():
    offenders = []
    for kind in _spawning_kinds():
        if kind in KINDS_WITHOUT_PERMISSION_AXES:
            continue
        spec = provider_manifest.spec_for(kind)
        assert spec is not None
        # Subclasses inherit their parent's start_run; check the module that
        # actually builds the payload.
        module = spec.module
        if _passes_permission_to_runner(module):
            continue
        parent_passes = any(
            _passes_permission_to_runner(other.module)
            for other in (provider_manifest.spec_for(k) for k in provider_manifest.all_kinds())
            if other is not None and other.module != module
            and other.runner_module == spec.runner_module
        )
        if not parent_passes:
            offenders.append(kind)
    assert not offenders, (
        f"these providers never put `permission` in their runner input, so the "
        f"runner cannot grant tool access: {offenders}"
    )


def test_agy_runner_fails_closed():
    import runner_agy

    assert runner_agy.permission_argv({"mode": "dangerously-skip-permissions"}) == [
        "--dangerously-skip-permissions"
    ]
    for denied in (None, {}, {"mode": "default"}, {"mode": ""}, "nonsense"):
        assert runner_agy.permission_argv(denied) == [], denied


if __name__ == "__main__":
    try:
        test_every_spawning_kind_has_a_permission_default()
        test_agy_defaults_to_full_permission()
        test_fugu_shares_codex_permission()
        test_every_spawning_provider_passes_permission_into_run_inputs()
        test_agy_runner_fails_closed()
        print("OK")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
