"""ZaiOpenAIProvider — Z.AI via its OpenAI-compatible endpoint, driven by Codex.

Z.AI's native `/api/paas/v4` endpoint speaks OpenAI Chat Completions and is
where Z.AI's automatic context caching is reported
(`usage.prompt_tokens_details.cached_tokens`). The Anthropic-compatible
endpoint used by the `claude` kind does not surface that caching, so this
provider routes through the Codex CLI instead: a `zai-openai` provider
profile in `~/.codex/config.toml` (`wire_api = "chat"`) plus a `codex-zai`
launcher that runs `codex -p zai-openai`, forwarding every argument to codex
unchanged. This mirrors the Fugu provider (provider_fugu.py): Z.AI-OpenAI is
Codex with a different profile, binary, and model catalog — it reuses
`CodexProvider` and `runner_codex` wholesale.

Setup is manual: create the `codex-zai` launcher and the `zai-openai` profile
(see docs), then add a Z.AI (OpenAI) provider.
"""

from __future__ import annotations

from typing import ClassVar

from provider_codex import CodexProvider


# Static catalog (authoritative list from Z.AI's /models endpoint). Used as the
# fallback when `codex-zai debug models` is unavailable.
ZAI_OPENAI_MODELS = [
    "glm-5.2",
    "glm-5.1",
    "glm-5",
    "glm-5-turbo",
    "glm-4.7",
    "glm-4.6",
    "glm-4.5-air",
    "glm-4.5",
]


def fetch_zai_openai_models() -> list[str]:
    """Best-effort model list from `codex-zai debug models`.

    Returns the static `ZAI_OPENAI_MODELS` list on any failure (binary
    missing, non-zero exit, parse error) so the dropdown always has something.
    """
    import json as _json
    import subprocess as _sp

    from cli_paths import resolve_cli_binary

    zai_bin = resolve_cli_binary("codex-zai")
    if not zai_bin:
        return list(ZAI_OPENAI_MODELS)

    try:
        proc = _sp.run(
            [zai_bin, "debug", "models"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, _sp.TimeoutExpired):
        return list(ZAI_OPENAI_MODELS)

    if proc.returncode != 0:
        return list(ZAI_OPENAI_MODELS)

    try:
        data = _json.loads(proc.stdout)
        models = [
            m["slug"]
            for m in data.get("models", [])
            if m.get("visibility") != "hide" and m.get("slug")
        ]
        return models if len(models) >= 1 else list(ZAI_OPENAI_MODELS)
    except (ValueError, KeyError, TypeError):
        return list(ZAI_OPENAI_MODELS)


class ZaiOpenAIProvider(CodexProvider):
    """Z.AI over its OpenAI Chat Completions endpoint — drives the `codex-zai`
    launcher, which is `codex -p zai-openai` under the hood. Inherits all
    Codex app-server behavior (fork, steering, subagents); only the binary,
    profile, and model catalog differ. Routing through Codex (rather than the
    `claude` kind) is what makes Z.AI's automatic context caching visible."""

    KIND: ClassVar[str] = "zai-openai"
    RUNNER_KIND: ClassVar[str] = "zai-openai"
    CODEX_BINARY: ClassVar[str] = "codex-zai"

    # The codex profile's `env_key` — the env var codex reads the Z.AI key
    # from. Must match `[model_providers.zai-openai]` in ~/.codex/config.toml.
    API_KEY_ENV: ClassVar[str] = "ZAI_API_KEY"

    def build_env(self) -> dict[str, str]:
        # CodexProvider.build_env assumes codex's own auth (subscription).
        # Z.AI-OpenAI is api_key mode: inject the key into the profile's
        # env_key so codex can authenticate against the Z.AI endpoint.
        env = super().build_env()
        record = self.record  # atomic snapshot
        if record.get("mode") == "api_key":
            api_key = record.get("api_key") or ""
            if api_key:
                env[self.API_KEY_ENV] = api_key
        return env
