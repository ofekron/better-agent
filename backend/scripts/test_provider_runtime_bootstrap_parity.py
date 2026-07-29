#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from provider_runtime_bootstrap_audit import (
    ProviderTarget,
    audit_targets,
    manifest_targets,
    targets_from_specs,
)


def _write_provider_fixture(root: Path, name: str, source: str) -> None:
    (root / f"{name}.py").write_text(source, encoding="utf-8")


def _test_provider_classification() -> None:
    with tempfile.TemporaryDirectory(prefix="provider-runtime-audit-") as tmp:
        root = Path(tmp)
        fixtures = {
            "provider": """
from abc import abstractmethod
class Provider:
    @abstractmethod
    def _start_run(self): ...
""",
            "provider_shared": """
from provider import Provider
class SharedProvider(Provider):
    pass
""",
            "provider_direct": """
from provider import Provider
class DirectProvider(Provider):
    def _start_run(self, run_id):
        env = build_better_agent_run_env(run_id=run_id)
        spawn(runner_argv(), env=env)
""",
            "provider_indirect": """
import provider_shared as shared
class IndirectProvider(shared.SharedProvider):
    def _start_run(self, run_id):
        env = provider_api.build_better_agent_run_env(run_id=run_id)
        asyncio.create_subprocess_exec(runner_argv(), env=env)
""",
            "provider_helper_bypass": """
from provider_shared import SharedProvider
class HelperBypassProvider(SharedProvider):
    def _start_run(self, run_id):
        self._launch()
    def _launch(self):
        spawn(runner_argv(), env={})
""",
            "provider_helper_good": """
from provider_shared import SharedProvider
class HelperGoodProvider(SharedProvider):
    def _start_run(self, run_id):
        self._launch(run_id)
    def _launch(self, run_id):
        env = build_better_agent_run_env(run_id=run_id)
        spawn(runner_argv(), env=env)
""",
            "provider_mixed_helpers": """
from provider_shared import SharedProvider
class MixedHelpersProvider(SharedProvider):
    def _start_run(self, run_id):
        self._good(run_id)
        self._bypass()
    def _good(self, run_id):
        env = build_better_agent_run_env(run_id=run_id)
        spawn(runner_argv(), env=env)
    def _bypass(self):
        spawn(runner_argv(), env={})
""",
            "provider_nested_helpers": """
from provider_shared import SharedProvider
class NestedHelpersProvider(SharedProvider):
    def _start_run(self, run_id):
        env = build_better_agent_run_env(run_id=run_id)
        spawn(runner_argv(), env=env)
        self._prepare()
    def _prepare(self):
        self._bypass()
    def _bypass(self):
        spawn(runner_argv(), env={})
""",
            "provider_missing_run_id": """
from provider_shared import SharedProvider
class MissingRunIdProvider(SharedProvider):
    def _start_run(self, run_id):
        env = build_better_agent_run_env()
        spawn(runner_argv(), env=env)
""",
            "provider_overwritten_env": """
from provider_shared import SharedProvider
class OverwrittenEnvProvider(SharedProvider):
    def _start_run(self, run_id):
        env = build_better_agent_run_env(run_id=run_id)
        env = {}
        spawn(runner_argv(), env=env)
""",
            "provider_updated_env": """
from provider_shared import SharedProvider
class UpdatedEnvProvider(SharedProvider):
    def _start_run(self, run_id, extra_env):
        env = build_better_agent_run_env(run_id=run_id)
        env.update(extra_env)
        spawn(runner_argv(), env=env)
""",
            "provider_transformed_env": """
from provider_shared import SharedProvider
class TransformedEnvProvider(SharedProvider):
    def _start_run(self, run_id):
        env = build_better_agent_run_env(run_id=run_id)
        env = replace(env)
        spawn(runner_argv(), env=env)
""",
            "provider_aliased_argv": """
from provider_shared import SharedProvider
class AliasedArgvProvider(SharedProvider):
    def _start_run(self, run_id):
        env = build_better_agent_run_env(run_id=run_id)
        spawn(runner_argv(), env=env)
        argv = runner_argv()
        spawn(argv, env={})
""",
            "provider_nested_argv": """
from provider_shared import SharedProvider
class NestedArgvProvider(SharedProvider):
    def _start_run(self, run_id, ready):
        env = build_better_agent_run_env(run_id=run_id)
        spawn(runner_argv(), env=env)
        argv = runner_argv()
        if ready:
            spawn(argv, env={})
""",
            "provider_pinned_argv": """
from provider_shared import SharedProvider
class PinnedArgvProvider(SharedProvider):
    def _start_run(self, run_id, launch):
        env = build_better_agent_run_env(run_id=run_id)
        spawn(runner_argv(), env=env)
        with launch.open_runner() as pinned:
            spawn(list(pinned.argv), env={})
""",
            "provider_unimplemented": """
from provider_shared import SharedProvider
class UnimplementedProvider(SharedProvider):
    pass
""",
            "provider_base": """
from provider import Provider
class BaseProvider(Provider):
    def _start_run(self, run_id):
        spawn(runner_argv(), env={})
""",
            "provider_mid": """
from provider_base import BaseProvider
class MidProvider(BaseProvider):
    pass
""",
            "provider_override": """
from provider_base import BaseProvider
class OverrideProvider(BaseProvider):
    def _start_run(self, run_id):
        env = build_better_agent_run_env(run_id=run_id)
        spawn(runner_argv(), env=env)
""",
            "provider_diamond": """
from provider_mid import MidProvider
from provider_override import OverrideProvider
class DiamondProvider(MidProvider, OverrideProvider):
    pass
""",
            "provider_template": """
from provider import Provider
class TemplateProvider(Provider):
    def _start_run(self, run_id):
        self._launch(run_id)
    def _launch(self, run_id):
        env = build_better_agent_run_env(run_id=run_id)
        spawn(runner_argv(), env=env)
""",
            "provider_concrete_override": """
from provider_template import TemplateProvider
class ConcreteOverrideProvider(TemplateProvider):
    def _launch(self, run_id):
        spawn(runner_argv(), env={})
""",
        }
        for name, source in fixtures.items():
            _write_provider_fixture(root, name, source)

        selected = targets_from_specs(
            {
                "local": SimpleNamespace(
                    module="provider_direct",
                    cls="DirectProvider",
                    runner_module="runner",
                    virtual=False,
                ),
                "remote": SimpleNamespace(
                    module="provider_proxy",
                    cls="ProxyProvider",
                    runner_module=None,
                    virtual=True,
                ),
                "conflicting-remote": SimpleNamespace(
                    module="provider_proxy",
                    cls="ProxyProvider",
                    runner_module="runner",
                    virtual=True,
                ),
            }
        )
        assert selected == [
            ProviderTarget("local", "provider_direct", "DirectProvider"),
            ProviderTarget(
                "conflicting-remote",
                "provider_proxy",
                "ProxyProvider",
            ),
        ]
        targets = [
            ProviderTarget("direct", "provider_direct", "DirectProvider"),
            ProviderTarget("indirect", "provider_indirect", "IndirectProvider"),
            ProviderTarget(
                "helper-good",
                "provider_helper_good",
                "HelperGoodProvider",
            ),
            ProviderTarget(
                "helper-bypass",
                "provider_helper_bypass",
                "HelperBypassProvider",
            ),
            ProviderTarget(
                "mixed-helpers",
                "provider_mixed_helpers",
                "MixedHelpersProvider",
            ),
            ProviderTarget(
                "nested-helpers",
                "provider_nested_helpers",
                "NestedHelpersProvider",
            ),
            ProviderTarget(
                "missing-run-id",
                "provider_missing_run_id",
                "MissingRunIdProvider",
            ),
            ProviderTarget(
                "unimplemented",
                "provider_unimplemented",
                "UnimplementedProvider",
            ),
            ProviderTarget(
                "overwritten-env",
                "provider_overwritten_env",
                "OverwrittenEnvProvider",
            ),
            ProviderTarget(
                "updated-env",
                "provider_updated_env",
                "UpdatedEnvProvider",
            ),
            ProviderTarget(
                "transformed-env",
                "provider_transformed_env",
                "TransformedEnvProvider",
            ),
            ProviderTarget(
                "aliased-argv",
                "provider_aliased_argv",
                "AliasedArgvProvider",
            ),
            ProviderTarget(
                "nested-argv",
                "provider_nested_argv",
                "NestedArgvProvider",
            ),
            ProviderTarget(
                "pinned-argv",
                "provider_pinned_argv",
                "PinnedArgvProvider",
            ),
            ProviderTarget(
                "diamond",
                "provider_diamond",
                "DiamondProvider",
            ),
            ProviderTarget(
                "concrete-override",
                "provider_concrete_override",
                "ConcreteOverrideProvider",
            ),
        ]
        missing_launchers, missing_runtime_env, calls_without_run_id = (
            audit_targets(root, targets)
        )
        assert missing_launchers == [
            "unimplemented:provider_unimplemented.UnimplementedProvider"
        ]
        assert missing_runtime_env == [
            "provider_aliased_argv.py:8",
            "provider_concrete_override.py:5",
            "provider_helper_bypass.py:7",
            "provider_mixed_helpers.py:11",
            "provider_nested_argv.py:9",
            "provider_nested_helpers.py:11",
            "provider_overwritten_env.py:7",
            "provider_pinned_argv.py:8",
            "provider_transformed_env.py:7",
            "provider_updated_env.py:7",
        ], missing_runtime_env
        assert calls_without_run_id == ["provider_missing_run_id.py:5"]


def main() -> None:
    _test_provider_classification()
    missing_launchers, providers_without_runtime_env, calls_without_run_id = (
        audit_targets(BACKEND_DIR, manifest_targets())
    )
    assert not missing_launchers, missing_launchers
    assert not providers_without_runtime_env, providers_without_runtime_env
    assert not calls_without_run_id, calls_without_run_id
    print("provider runtime bootstrap parity: OK")


if __name__ == "__main__":
    main()
