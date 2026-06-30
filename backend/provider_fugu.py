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


def fetch_fugu_models() -> list[str]:
    """Best-effort model list from Codex with the Sakana model provider.

    Returns the static `FUGU_MODELS` list on any failure (codex missing,
    provider not installed, non-zero exit, parse error) so the dropdown
    always has something. Fugu exposes exactly two models — Fugu and Fugu
    Ultra — so the static list is authoritative in practice.
    """
    import json as _json
    import subprocess as _sp

    from cli_paths import resolve_cli_binary

    codex_bin = resolve_cli_binary("codex")
    if not codex_bin:
        return list(FUGU_MODELS)

    try:
        proc = _sp.run(
            [
                codex_bin,
                "-c", "model_provider=\"sakana\"",
                "-c", "model=\"fugu\"",
                "debug", "models",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, _sp.TimeoutExpired):
        return list(FUGU_MODELS)

    if proc.returncode != 0:
        return list(FUGU_MODELS)

    try:
        data = _json.loads(proc.stdout)
        allowed = set(FUGU_MODELS)
        models = [
            m["slug"]
            for m in data.get("models", [])
            if m.get("visibility") != "hide" and m.get("slug") in allowed
        ]
        return models if len(models) >= 1 else list(FUGU_MODELS)
    except (ValueError, KeyError, TypeError):
        return list(FUGU_MODELS)


class FuguProvider(CodexProvider):
    """Sakana Fugu — drives the regular `codex` binary with the `fugu`
    model provider selected via `-c`. Inherits all Codex app-server
    behavior (fork, steering, subagents); only the config overrides and model
    catalog differ."""

    KIND: ClassVar[str] = "fugu"
    RUNNER_KIND: ClassVar[str] = "fugu"
    CODEX_PROFILE: ClassVar[Optional[str]] = None
    CODEX_MODEL_PROVIDER: ClassVar[str] = "sakana"

    # Sakana's Fugu catalog advertises exactly two
    # reasoning levels for both Fugu and Fugu Ultra — `high` and `xhigh`.
    # The model provider override routes the call to Fugu, so codex's
    # `model_reasoning_effort` config reaches the model; expose the dial.
    supports_reasoning_effort: ClassVar[bool] = True
    reasoning_effort_options: ClassVar[tuple[str, ...]] = ("high", "xhigh")
    default_reasoning_effort: ClassVar[str] = "high"

    def codex_config_overrides(self, *, model: Optional[str]) -> list[str]:
        selected_model = model if model in FUGU_MODELS else FUGU_MODELS[0]
        return [
            f"model_provider={toml_literal(self.CODEX_MODEL_PROVIDER)}",
            f"model={toml_literal(selected_model)}",
        ]
