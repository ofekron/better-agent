"""FuguProvider — Sakana Fugu via the regular `codex` CLI.

Fugu (https://sakana.ai/fugu/) is a multi-agent system exposed as an
OpenAI-compatible API that plugs into the Codex CLI. Sakana's installer
deploys a `sakana` model provider into `~/.codex/config.toml`. We select it
with Codex `-c` config overrides, reusing the same `codex` binary the generic
Codex provider already drives — no separate launcher binary is needed. Fugu
inherits `CodexProvider` and `runner_codex` wholesale; only the config
overrides and model catalog differ.

Setup is manual (the installer is a `git clone HEAD | bash` bootstrap that
is not hash-pinnable, so it is intentionally NOT wired into the setup
wizard). Users run the installer themselves (it writes the `sakana` model
provider), then add a Fugu provider.
"""

from __future__ import annotations

from typing import ClassVar, Optional

from provider_codex import CodexProvider
from provider_run_config import toml_literal


FUGU_MODELS = [
    "fugu",
    "fugu-ultra",
]


class FuguProvider(CodexProvider):
    """Sakana Fugu — drives the regular `codex` binary with the `fugu`
    model provider selected via `-c`. Inherits all Codex app-server
    behavior (fork, steering, subagents); only the config overrides and model
    catalog differ."""

    KIND: ClassVar[str] = "fugu"
    RUNNER_KIND: ClassVar[str] = "fugu"
    CODEX_PROFILE: ClassVar[Optional[str]] = None
    CODEX_MODEL_PROVIDER: ClassVar[str] = "sakana"
    uses_managed_api_key: ClassVar[bool] = True

    # Sakana's Fugu catalog advertises exactly two
    # reasoning levels for both Fugu and Fugu Ultra — `high` and `xhigh`.
    # The model provider override routes the call to Fugu, so codex's
    # `model_reasoning_effort` config reaches the model; expose the dial.
    supports_reasoning_effort: ClassVar[bool] = True
    reasoning_effort_options: ClassVar[tuple[str, ...]] = ("high", "xhigh")
    default_reasoning_effort: ClassVar[str] = "high"

    def codex_config_overrides(self, *, model: Optional[str]) -> list[str]:
        if model not in FUGU_MODELS:
            raise ValueError("Fugu model must be one of the configured models")
        selected_model = model
        return [
            f"model_provider={toml_literal(self.CODEX_MODEL_PROVIDER)}",
            f"model={toml_literal(selected_model)}",
            # Codex ships the image_generation tool by default (stable feature),
            # but Sakana's Responses API only accepts `function`/`custom` tool
            # types and rejects `image_generation` with an invalid_request_error
            # on every turn. Disable the feature for Fugu runs.
            "features.image_generation=false",
            "features.shell_snapshot=false",
            f"shell_environment_policy.exclude={toml_literal(['SAKANA_API_KEY'])}",
        ]

    def build_env(self) -> dict[str, str]:
        env = super().build_env()
        env.pop("SAKANA_API_KEY", None)
        api_key = self.runtime_record().get("api_key")
        if isinstance(api_key, str) and api_key:
            env["SAKANA_API_KEY"] = api_key
        return env
